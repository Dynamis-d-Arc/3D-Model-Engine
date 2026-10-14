#include "TwinAirflowActor.h"

#include "Components/InstancedStaticMeshComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/StaticMesh.h"
#include "Engine/Texture2D.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Misc/CommandLine.h"
#include "ProceduralMeshComponent.h"
#include "TwinViewerDirector.h"

namespace
{
	// school-flow.js / illustration.js / thermal.js, in Unreal cm
	constexpr int32 Samples = 192;          // per closed loop
	constexpr int32 LoopsPerOutlet = 6;
	constexpr float LoopsPerSecond = 0.065f;
	constexpr float DotScale = 0.035f;      // the engine sphere is 100 cm across -> 3.5 cm dots
	constexpr float CoolingKW = 7.f, InputKW = 2.f, FanKW = 0.06f, Air = 1.2f * 1005.f;
	constexpr int32 MaxOutlets = 4;         // O0..O3 / D0..D3 in M_TwinMist

	bool V3(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, FVector& Out)
	{
		const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
		if (!O.IsValid() || !O->TryGetArrayField(Key, A) || A->Num() < 3)
		{
			return false;
		}
		Out = FVector((*A)[0]->AsNumber(), (*A)[1]->AsNumber(), (*A)[2]->AsNumber());
		return true;
	}

	float Num(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, float Default)
	{
		double V = 0.0;
		return O.IsValid() && O->TryGetNumberField(Key, V) ? (float)V : Default;
	}

	/** A unit's air temperature from the feed: room sensor, else return air (thermal.js `reading`). */
	float AirTemp(const FTwinHvacReading* R)
	{
		if (!R)
		{
			return NAN;
		}
		if (!FMath::IsNaN(R->RoomC) && R->RoomC > 0.f)
		{
			return R->RoomC;
		}
		return (!FMath::IsNaN(R->ReturnC) && R->ReturnC > 0.f) ? R->ReturnC : NAN;
	}

	/** How much colder the jet core is than the room (thermal.js zoneValues / liveState). */
	float DropFor(float T, float CoolingW, float Lps)
	{
		return FMath::Min(FMath::Max(0.f, T - 12.f), CoolingW / (FMath::Max(Lps, 1.f) / 1000.f * Air));
	}

	FLinearColor Lin(const FVector& V, float W = 0.f) { return FLinearColor(V.X, V.Y, V.Z, W); }
}

ATwinAirflowActor::ATwinAirflowActor()
{
	PrimaryActorTick.bCanEverTick = true;
	Dots = CreateDefaultSubobject<UInstancedStaticMeshComponent>(TEXT("Dots"));
	RootComponent = Dots;
	Dots->SetMobility(EComponentMobility::Movable);
	Dots->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	Dots->SetCanEverAffectNavigation(false);
	Dots->SetCastShadow(false);
	Dots->NumCustomDataFloats = 3;          // RGB read by M_TwinFlow (PerInstanceCustomData 0..2)
	Dots->TranslucencySortPriority = 10;    // tracers over the mist, as on the web
	Dots->SetVisibility(false);
}

void ATwinAirflowActor::BeginPlay()
{
	Super::BeginPlay();
	// loaded by path (cooked via DirectoriesToAlwaysCook) so the editor works before the viewer content exists
	if (UStaticMesh* Sphere = LoadObject<UStaticMesh>(nullptr, TEXT("/Engine/BasicShapes/Sphere.Sphere")))
	{
		Dots->SetStaticMesh(Sphere);
	}
	if (UMaterialInterface* Flow = LoadObject<UMaterialInterface>(nullptr, TEXT("/Game/Viewer/Materials/M_TwinFlow.M_TwinFlow")))
	{
		Dots->SetMaterial(0, Flow);
	}
	MistBase = LoadObject<UMaterialInterface>(nullptr, TEXT("/Game/Viewer/Materials/M_TwinMist.M_TwinMist"));
	// look tuning without a repackage
	FParse::Value(FCommandLine::Get(), TEXT("TwinMistGain="), MistGain);
	FParse::Value(FCommandLine::Get(), TEXT("TwinMistDensity="), MistDensity);
	FParse::Value(FCommandLine::Get(), TEXT("TwinMistDots="), DotsPerPath);
	DotsPerPath = FMath::Clamp(DotsPerPath, 1, 32);
}

FLinearColor ATwinAirflowActor::HeatColor(float C)
{
	struct FStop { float U; FVector C; };
	static const FStop Stops[] = {{0.f, FVector(0.28, 0.16, 0.55)}, {0.2f, FVector(0.04, 0.65, 0.95)}, {0.42f, FVector(0.13, 0.94, 0.64)},
	                              {0.64f, FVector(0.85, 0.93, 0.28)}, {0.82f, FVector(1.0, 0.56, 0.23)}, {1.f, FVector(0.94, 0.2, 0.19)}};
	const float U = FMath::Clamp((C - 12.f) / 24.f, 0.f, 1.f);
	FVector Out = Stops[UE_ARRAY_COUNT(Stops) - 1].C;
	for (int32 i = 1; i < UE_ARRAY_COUNT(Stops); ++i)
	{
		if (U <= Stops[i].U)
		{
			Out = FMath::Lerp(Stops[i - 1].C, Stops[i].C, (U - Stops[i - 1].U) / (Stops[i].U - Stops[i - 1].U));
			break;
		}
	}
	// the ramp is in display (sRGB) values; the canvas and the mist shader work in linear colour
	return FLinearColor(FMath::Pow((float)Out.X, 2.2f), FMath::Pow((float)Out.Y, 2.2f), FMath::Pow((float)Out.Z, 2.2f));
}

void ATwinAirflowActor::Clear()
{
	for (UProceduralMeshComponent* M : MistMeshes)
	{
		if (M)
		{
			M->DestroyComponent();
		}
	}
	MistMeshes.Reset();
	MistMaterials.Reset();
	Mask = nullptr;
	MaskDims = FIntPoint::ZeroValue;
	MaskRect = FVector4f(0, 0, 0, 0);
	Zones.Reset();
	Paths.Reset();
	Xf.Reset();
	Outline.Reset();
	Thermal.Reset();
	DriverName = TEXT("none");
	LastTelemetry = -1.0;
	Visible = 0;
	Dots->ClearInstances();
	Dots->SetVisibility(false);
	bShown = false;
}

void ATwinAirflowActor::SetShown(bool bInShown)
{
	bShown = bInShown && Xf.Num() > 0;
	ApplyVisibility();
}

void ATwinAirflowActor::SetMistShown(bool bInShown)
{
	bMistShown = bInShown;
	ApplyVisibility();
}

void ATwinAirflowActor::SetFloorHidden(bool bInHidden)
{
	bFloorHidden = bInHidden;
	ApplyVisibility();
}

int32 ATwinAirflowActor::NumMistZones() const
{
	return MistMeshes.Num();
}

int32 ATwinAirflowActor::VisibleMistZones() const
{
	int32 N = 0;
	for (const FZone& Z : Zones)
	{
		N += (Z.Mesh && Z.Mesh->IsVisible()) ? 1 : 0;
	}
	return N;
}

FIntPoint ATwinAirflowActor::MaskSize() const
{
	return MaskDims;
}

FVector2D ATwinAirflowActor::TemperatureRange() const
{
	float Lo = TNumericLimits<float>::Max(), Hi = -TNumericLimits<float>::Max();
	for (const FZone& Z : Zones)
	{
		if (!Z.bHidden && !FMath::IsNaN(Z.Temp))
		{
			Lo = FMath::Min(Lo, Z.Temp);
			Hi = FMath::Max(Hi, Z.Temp);
		}
	}
	return Lo <= Hi ? FVector2D(Lo, Hi) : FVector2D(NAN, NAN);
}

bool ATwinAirflowActor::Inside(const FVector2D& P) const
{
	if (Outline.Num() < 3)
	{
		return true;
	}
	bool bIn = false;
	for (int32 i = 0, j = Outline.Num() - 1; i < Outline.Num(); j = i++)
	{
		const FVector2D& A = Outline[i];
		const FVector2D& B = Outline[j];
		if ((A.Y > P.Y) != (B.Y > P.Y) && P.X < (B.X - A.X) * (P.Y - A.Y) / (B.Y - A.Y) + A.X)
		{
			bIn = !bIn;
		}
	}
	return bIn;
}

FVector ATwinAirflowActor::Contain(const FZone& Z, FVector P, float Top) const
{
	// school-flow.js contain(): into the zone box (12 cm inset, 22 cm above the floor .. top), then towards the
	// box centre until the point is inside the floor outline
	const FVector2D C = Z.Box.GetCenter();
	P.X = FMath::Clamp(P.X, Z.Box.Min.X + 12.f, Z.Box.Max.X - 12.f);
	P.Y = FMath::Clamp(P.Y, Z.Box.Min.Y + 12.f, Z.Box.Max.Y - 12.f);
	P.Z = FMath::Clamp(P.Z, 22.f, Top);
	for (int32 n = 0; n < 25 && !Inside(FVector2D(P.X, P.Y)); ++n)
	{
		P.X = P.X * 0.85f + C.X * 0.15f;
		P.Y = P.Y * 0.85f + C.Y * 0.15f;
	}
	return P;
}

void ATwinAirflowActor::Build(const TSharedPtr<FJsonObject>& Airflow, ATwinViewerDirector* InDirector, const FString& InUnitPrefix,
                              const FString& InFloorKey)
{
	Clear();
	Director = InDirector;
	UnitPrefix = InUnitPrefix;
	Floor = InFloorKey;
	if (!Airflow.IsValid())
	{
		return;
	}
	OutlineBox = FBox2D(ForceInit);
	const TArray<TSharedPtr<FJsonValue>>* OL = nullptr;
	if (Airflow->TryGetArrayField(TEXT("outline_cm"), OL))
	{
		for (const TSharedPtr<FJsonValue>& PV : *OL)
		{
			const TArray<TSharedPtr<FJsonValue>>& P = PV->AsArray();
			if (P.Num() >= 2)
			{
				Outline.Add(FVector2D(P[0]->AsNumber(), P[1]->AsNumber()));
				OutlineBox += Outline.Last();
			}
		}
	}
	const TSharedPtr<FJsonObject>* Th = nullptr;
	if (Airflow->TryGetObjectField(TEXT("thermal"), Th))
	{
		Thermal = *Th;
	}
	for (const TSharedPtr<FJsonValue>& ZV : Airflow->GetArrayField(TEXT("zones")))
	{
		const TSharedPtr<FJsonObject> J = ZV->AsObject();
		FZone Z;
		Z.UnitId = J->GetStringField(TEXT("unit_id"));
		Z.Ceiling = Num(J, TEXT("ceiling_cm"), 280.f);
		Z.AirflowLps = Num(J, TEXT("airflow_lps"), 240.f);
		Z.Box = FBox2D(ForceInit);
		const TArray<TSharedPtr<FJsonValue>>& BA = J->GetArrayField(TEXT("box_cm"));
		if (BA.Num() == 2)
		{
			Z.Box += FVector2D(BA[0]->AsArray()[0]->AsNumber(), BA[0]->AsArray()[1]->AsNumber());
			Z.Box += FVector2D(BA[1]->AsArray()[0]->AsNumber(), BA[1]->AsArray()[1]->AsNumber());
		}
		FVector Unit;
		if (!Z.Box.bIsValid || !V3(J, TEXT("unit_cm"), Unit))
		{
			continue;
		}
		Z.UnitXY = FVector2D(Unit.X, Unit.Y);
		if (!V3(J, TEXT("outlet_cm"), Z.Outlet))
		{
			Z.Outlet = FVector(Unit.X, Unit.Y, Z.Ceiling - 5.f);
		}
		const TArray<TSharedPtr<FJsonValue>>* D = nullptr;
		Z.Dir = J->TryGetArrayField(TEXT("outlet_dir"), D) && D->Num() >= 2
			        ? FVector((*D)[0]->AsNumber(), (*D)[1]->AsNumber(), 0.0).GetSafeNormal()
			        : FVector::ZeroVector;
		Zones.Add(MoveTemp(Z));
	}
	SplitX = OutlineBox.bIsValid ? (float)OutlineBox.GetCenter().X : 0.f;
	if (Thermal.IsValid())
	{
		SplitX = Num(Thermal, TEXT("split_x_cm"), SplitX);
	}
	for (int32 i = 0; i < Zones.Num(); ++i)
	{
		LayoutZone(i);
	}
	if (Xf.Num() == 0)
	{
		return;
	}
	Dots->AddInstances(Xf, false, false, false);  // component space: the actor carries the floor offset
	for (int32 i = 0; i < Xf.Num(); ++i)  // the web's pale tracer colour; the mist carries the temperature
	{
		Dots->SetCustomDataValue(i, 0, 0.89f);
		Dots->SetCustomDataValue(i, 1, 1.0f);
		Dots->SetCustomDataValue(i, 2, 0.97f);
	}
	Dots->MarkRenderStateDirty();
	BuildMask();
	BuildMist();
	UpdateDrivers();
	ApplyVisibility();
}

void ATwinAirflowActor::LayoutZone(int32 ZoneIndex)
{
	FZone& Z = Zones[ZoneIndex];
	const float Top = Z.Ceiling - 5.f;  // the outlet sits just below the ceiling (ducted casings hang above it)
	const FVector O = Contain(Z, Z.Outlet, Top);
	FVector Dir = Z.Dir;
	if (Dir.IsNearlyZero())  // no known discharge face: leave along +-X / +-Y, away from the nearest box edge
	{
		const FVector B = Contain(Z, FVector(Z.UnitXY.X, Z.UnitXY.Y, 280.f), 285.f);
		const float Dist[] = {(float)(B.Y - Z.Box.Min.Y), (float)(Z.Box.Max.Y - B.Y), (float)(B.X - Z.Box.Min.X), (float)(Z.Box.Max.X - B.X)};
		const FVector Dirs[] = {FVector(0, 1, 0), FVector(0, -1, 0), FVector(1, 0, 0), FVector(-1, 0, 0)};
		int32 Best = 0;
		for (int32 i = 1; i < 4; ++i)
		{
			Best = Dist[i] < Dist[Best] ? i : Best;
		}
		Dir = Dirs[Best];
	}
	Z.Outlet = O;
	Z.Dir = Dir;

	// closed tracer loops: leave the louvres level, fall to 1.6 m and 0.65 m, back along the floor, up again
	const FVector2D Size = Z.Box.GetSize();
	const bool bAlongX = FMath::Abs(Dir.X) > 0.5f;
	const float Reach = FMath::Min((bAlongX ? Size.X : Size.Y) * 0.82f, 850.f);
	for (int32 j = 0; j < LoopsPerOutlet; ++j)
	{
		const float Side = (j - 2.5f) * FMath::Min(24.f, (float)(bAlongX ? Size.Y : Size.X) / 18.f);
		auto At = [&](float T, float H, float Offset = 0.f)
		{
			return Contain(Z, FVector(O.X + Dir.X * Reach * T + Dir.Y * (Side + Offset), O.Y + Dir.Y * Reach * T - Dir.X * (Side + Offset), H), Top);
		};
		const TArray<FVector> Nodes = {O, At(0.12f, O.Z - 25.f), At(0.3f, FMath::Min(O.Z - 90.f, 240.f)), At(0.6f, 150.f), At(0.88f, 65.f),
		                               At(0.65f, 35.f, Side * 0.6f), At(0.25f, 65.f, Side), At(0.06f, 175.f, Side)};
		FPath P;
		P.Zone = ZoneIndex;
		P.Index = j;
		P.Samples.Reserve(Samples);
		const int32 N = Nodes.Num();
		for (int32 k = 0; k < Samples; ++k)
		{
			const float U = (float)k / Samples * N;
			const int32 I = FMath::FloorToInt(U);
			const float T = U - I, T2 = T * T, T3 = T2 * T;
			const FVector& P0 = Nodes[(I + N - 1) % N];
			const FVector& P1 = Nodes[I % N];
			const FVector& P2 = Nodes[(I + 1) % N];
			const FVector& P3 = Nodes[(I + 2) % N];
			const FVector C = 0.5f * ((2.f * P1) + (-P0 + P2) * T + (2.f * P0 - 5.f * P1 + 4.f * P2 - P3) * T2 + (-P0 + 3.f * P1 - 3.f * P2 + P3) * T3);
			P.Samples.Add(Contain(Z, C, Top));
		}
		P.First = Xf.Num();
		for (int32 k = 0; k < DotsPerPath; ++k)
		{
			Xf.Add(FTransform(FQuat::Identity, P.Samples[0], FVector(DotScale)));
		}
		Paths.Add(MoveTemp(P));
	}
}

void ATwinAirflowActor::BuildMask()
{
	// the floor outline as a coverage texture, so the mist never leaves the building (the web shader tests the
	// polygon per sample; one texture fetch is cheaper)
	if (Outline.Num() < 3 || !OutlineBox.bIsValid)
	{
		return;  // the material's default mask (white, rect 0) keeps every sample
	}
	const FVector2D Span = OutlineBox.GetSize();
	const float Cell = FMath::Max(10.f, (float)FMath::Max(Span.X, Span.Y) / 510.f);
	const int32 W = FMath::CeilToInt(Span.X / Cell) + 2, H = FMath::CeilToInt(Span.Y / Cell) + 2;
	const FVector2D O = OutlineBox.Min - FVector2D(Cell, Cell);
	UTexture2D* T = UTexture2D::CreateTransient(W, H, PF_G8, NAME_None);  // no data: UpdateResource once, below
	if (!T)
	{
		return;
	}
	FTexture2DMipMap& Mip = T->GetPlatformData()->Mips[0];
	uint8* Dst = static_cast<uint8*>(Mip.BulkData.Lock(LOCK_READ_WRITE));
	for (int32 y = 0; y < H; ++y)
	{
		for (int32 x = 0; x < W; ++x)
		{
			Dst[y * W + x] = Inside(O + FVector2D((x + 0.5f) * Cell, (y + 0.5f) * Cell)) ? 255 : 0;
		}
	}
	Mip.BulkData.Unlock();
	T->SRGB = false;
	T->Filter = TF_Bilinear;
	T->AddressX = TA_Clamp;
	T->AddressY = TA_Clamp;
	T->UpdateResource();
	Mask = T;
	MaskDims = FIntPoint(W, H);
	MaskRect = FVector4f(O.X, O.Y, 1.f / (W * Cell), 1.f / (H * Cell));
}

void ATwinAirflowActor::BuildMist()
{
	bParamsOk = MistBase != nullptr;
	if (!MistBase)
	{
		return;  // viewer content without M_TwinMist: tracers only
	}
	for (FZone& Z : Zones)
	{
		const FVector Lo(Z.Box.Min.X + 10.f, Z.Box.Min.Y + 10.f, 15.f);
		const FVector Hi(Z.Box.Max.X - 10.f, Z.Box.Max.Y - 10.f, Z.Ceiling);
		// the box faces at their world positions (component at the origin): M_TwinMist marches from the camera
		// to the box exit seen on each back face; the material is two-sided and skips the front faces
		FProcMeshSection S;
		for (int32 i = 0; i < 8; ++i)
		{
			FProcMeshVertex& Vx = S.ProcVertexBuffer.AddDefaulted_GetRef();
			Vx.Position = FVector((i == 1 || i == 2 || i == 5 || i == 6) ? Hi.X : Lo.X, (i % 4 >= 2) ? Hi.Y : Lo.Y, i >= 4 ? Hi.Z : Lo.Z);
			S.SectionLocalBox += Vx.Position;
		}
		S.ProcIndexBuffer = {0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7};
		S.bEnableCollision = false;
		UProceduralMeshComponent* M = NewObject<UProceduralMeshComponent>(this, NAME_None, RF_Transient);
		M->SetupAttachment(RootComponent);
		M->SetMobility(EComponentMobility::Movable);
		M->SetCanEverAffectNavigation(false);
		M->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		M->SetCastShadow(false);
		M->SetProcMeshSection(0, S);
		UMaterialInstanceDynamic* Mid = UMaterialInstanceDynamic::Create(MistBase, this);
		Mid->SetVectorParameterValue(TEXT("Lo"), Lin(Lo));
		Mid->SetVectorParameterValue(TEXT("Hi"), Lin(Hi));
		Mid->SetVectorParameterValue(TEXT("O0"), Lin(Z.Outlet));
		Mid->SetVectorParameterValue(TEXT("D0"), Lin(Z.Dir));
		Mid->SetVectorParameterValue(TEXT("MaskRect"), FLinearColor(MaskRect.X, MaskRect.Y, MaskRect.Z, MaskRect.W));
		if (Mask)
		{
			Mid->SetTextureParameterValue(TEXT("Mask"), Mask);
		}
		for (const TCHAR* Name : {TEXT("Lo"), TEXT("Hi"), TEXT("O0"), TEXT("D0"), TEXT("Th"), TEXT("Look"), TEXT("MaskRect")})
		{
			FLinearColor Unused;
			bParamsOk &= Mid->GetVectorParameterValue(FHashedMaterialParameterInfo(Name), Unused);  // names as in M_TwinMist
		}
		M->SetMaterial(0, Mid);
		M->SetVisibility(false);
		M->RegisterComponent();
		Z.Mesh = M;
		Z.Mid = Mid;
		MistMeshes.Add(M);
		MistMaterials.Add(Mid);
	}
}

void ATwinAirflowActor::UpdateDrivers()
{
	const ATwinViewerDirector* D = Director.Get();
	const FTwinTelemetry* T = D ? &D->Telemetry : nullptr;
	LastTelemetry = T ? T->ReceivedAt : -1.0;
	const int32 Count = Zones.Num();
	float FloorLps = 0.f;
	for (const FZone& Z : Zones)
	{
		FloorLps += Z.AirflowLps;
	}
	bool bThermal = false;
	if (Thermal.IsValid())
	{
		Thermal->TryGetBoolField(TEXT("available"), bThermal);
	}
	const bool bFeed = T && (T->Source == TEXT("live") || T->Source == TEXT("simulated")) && T->Units.Num() > 0;
	JetGain = 1.f;
	if (bFeed)
	{
		// liveState(): per-half means of the readings; running units take their own temperature and a drop
		// from 7 kW of cooling over their design airflow; units that are off (or not reporting) are not drawn
		TArray<float> West, East, All;
		int32 Running = 0;
		for (const FZone& Z : Zones)
		{
			const FTwinHvacReading* R = D->ReadingFor(UnitPrefix + Z.UnitId);
			const float A = AirTemp(R);
			if (!FMath::IsNaN(A))
			{
				(Z.UnitXY.X < SplitX ? West : East).Add(A);
				All.Add(A);
			}
			Running += (R && R->bOn) ? 1 : 0;
		}
		auto Mean = [](const TArray<float>& V, float Def)
		{
			float S = 0.f;
			for (float X : V)
			{
				S += X;
			}
			return V.Num() ? S / V.Num() : Def;
		};
		const float Fallback = Mean(All, 27.f);
		const float W = Mean(West, Fallback), E = Mean(East, Fallback);
		const float FloorCoolingW = FMath::Max(0.f, Running * InputKW) * (CoolingKW / InputKW) * 1000.f;
		for (FZone& Z : Zones)
		{
			const FTwinHvacReading* R = D->ReadingFor(UnitPrefix + Z.UnitId);
			Z.Temp = Z.UnitXY.X < SplitX ? W : E;
			Z.Drop = DropFor(Z.Temp, FloorCoolingW, FloorLps);
			Z.bHidden = !(R && R->bOn);
			const float Own = AirTemp(R);
			if (!Z.bHidden && !FMath::IsNaN(Own))
			{
				Z.Temp = Own;
				Z.Drop = DropFor(Own, CoolingKW * 1000.f, Z.AirflowLps);
			}
		}
		DriverName = TEXT("feed");
	}
	else if (bThermal && (!T || !T->Source.IsEmpty()))
	{
		// MATLAB day: every unit takes its half's temperature and the floor's cooling drop; jets grow in the
		// first minute of the run
		const float W = Num(Thermal, TEXT("west_c"), 27.f), E = Num(Thermal, TEXT("east_c"), 27.f);
		const float Kw = Num(Thermal, TEXT("ac_kw"), 0.f);
		const float Minute = Num(Thermal, TEXT("minute"), 480.f);
		const float FloorCoolingW = FMath::Max(0.f, Kw - Count * FanKW) * (CoolingKW / InputKW) * 1000.f;
		JetGain = FMath::SmoothStep(0.f, 0.35f, Minute);
		for (FZone& Z : Zones)
		{
			Z.Temp = Z.UnitXY.X < SplitX ? W : E;
			Z.Drop = DropFor(Z.Temp, FloorCoolingW, FloorLps);
			Z.bHidden = false;
		}
		DriverName = TEXT("matlab");
	}
	else
	{
		for (FZone& Z : Zones)  // first feed poll still running, or no temperature anywhere: tracers only
		{
			Z.Temp = NAN;
			Z.bHidden = false;
		}
		DriverName = (T && T->Source.IsEmpty()) ? TEXT("waiting") : TEXT("none");
	}
	for (FZone& Z : Zones)
	{
		if (Z.Mid)
		{
			const float Tc = FMath::IsNaN(Z.Temp) ? 27.f : Z.Temp;
			Z.Mid->SetVectorParameterValue(TEXT("Th"), FLinearColor(Tc, Tc, Z.Drop, 1.f));
			Z.Mid->SetVectorParameterValue(TEXT("Look"), FLinearColor(JetGain, MistDensity, MistGain, 0.f));
		}
	}
}

void ATwinAirflowActor::ApplyVisibility()
{
	const bool bTemps = DriverName == TEXT("feed") || DriverName == TEXT("matlab");
	for (const FZone& Z : Zones)
	{
		if (Z.Mesh)
		{
			Z.Mesh->SetVisibility(bShown && !bFloorHidden && bMistShown && bTemps && !Z.bHidden);
		}
	}
	Dots->SetVisibility(bShown && !bFloorHidden);
}

void ATwinAirflowActor::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!bShown || bFloorHidden || Xf.Num() == 0)
	{
		return;
	}
	const ATwinViewerDirector* D = Director.Get();
	if (D && D->Telemetry.ReceivedAt != LastTelemetry)
	{
		UpdateDrivers();
		ApplyVisibility();
	}
	Clock += DeltaSeconds;
	Visible = 0;
	for (const FPath& P : Paths)
	{
		const bool bOff = Zones[P.Zone].bHidden;
		for (int32 k = 0; k < DotsPerPath; ++k)
		{
			const float U = FMath::Fractional((float)k / DotsPerPath + (float)(Clock * LoopsPerSecond) + (P.Index % 7) * 0.017f) * Samples;
			const int32 I = FMath::Min(FMath::FloorToInt(U), Samples - 1);
			const FVector Pos = FMath::Lerp(P.Samples[I], P.Samples[(I + 1) % Samples], U - I);
			Xf[P.First + k] = FTransform(FQuat::Identity, Pos, FVector(bOff ? 0.f : DotScale));
			Visible += bOff ? 0 : 1;
		}
	}
	Dots->BatchUpdateInstancesTransforms(0, Xf, false, true, true);
}
