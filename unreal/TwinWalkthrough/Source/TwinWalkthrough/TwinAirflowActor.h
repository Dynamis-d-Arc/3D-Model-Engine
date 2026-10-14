#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "TwinAirflowActor.generated.h"

class ATwinViewerDirector;
class FJsonObject;
class UInstancedStaticMeshComponent;
class UMaterialInstanceDynamic;
class UMaterialInterface;
class UProceduralMeshComponent;
class UTexture2D;

/**
 * Airflow ILLUSTRATION (not measured, not CFD), the same design as the web Live tab
 * (frontend/js/airflow/illustration.js, school-flow.js, thermal.js). One zone per AC unit
 * (GET .../hvac/airflow):
 *   - mist: a box around the unit (+/-3 m, floor to ceiling) raymarched by M_TwinMist in 16 steps; air
 *     temperature = the zone's temperature + 0.13 C/m above 1.5 m - drop x jet, the jet a Gaussian beam from
 *     the casing's discharge face that falls 2.1 m over its reach; 12-36 C colour ramp, a slow ripple; clipped
 *     to the floor outline (a mask texture) and stopped by walls and furniture (scene depth);
 *   - tracers: pale dots on 6 closed loops per outlet (along the ceiling, down into the room, back along the
 *     floor and up again), drawn over the mist.
 * Temperatures come from the telemetry feed (live BMS, else the simulated BMS - the HUD names which): units
 * that are off are not drawn, running units take their own air temperature and a cooling drop from their
 * design airflow. Without a feed, the MATLAB thermal model's west/east zones drive it (labelled simulation).
 * The HUD keeps a banner on screen whenever this layer is visible.
 */
UCLASS(NotPlaceable)
class TWINWALKTHROUGH_API ATwinAirflowActor : public AActor
{
	GENERATED_BODY()

public:
	ATwinAirflowActor();

	void Build(const TSharedPtr<FJsonObject>& Airflow, ATwinViewerDirector* InDirector);
	void Clear();
	void SetShown(bool bInShown);
	bool IsShown() const { return bShown; }
	void SetMistShown(bool bInShown);
	bool IsMistShown() const { return bMistShown; }
	int32 NumPaths() const { return Paths.Num(); }
	int32 NumDots() const { return Xf.Num(); }
	int32 NumUnits() const { return Zones.Num(); }
	int32 VisibleDots() const { return Visible; }
	int32 NumMistZones() const;
	int32 VisibleMistZones() const;
	bool HasMistMaterial() const { return MistBase != nullptr; }
	/** Every parameter the actor sets exists in M_TwinMist (a renamed one would silently keep its default). */
	bool MistParamsOk() const { return bParamsOk; }
	FIntPoint MaskSize() const;
	/** "feed" (live or simulated BMS), "matlab" (thermal model) or "none"; what colours the mist. */
	const FString& Driver() const { return DriverName; }
	/** Zone temperature range drawn now (min, max), NaN when nothing is drawn. */
	FVector2D TemperatureRange() const;

	virtual void Tick(float DeltaSeconds) override;
	virtual void BeginPlay() override;

	UPROPERTY(VisibleAnywhere, Category = "Twin")
	TObjectPtr<UInstancedStaticMeshComponent> Dots;

	/** The study's cloud colour ramp (12-36 C) as display (sRGB) values, for the HUD legend. */
	static FLinearColor HeatColor(float C);

private:
	struct FZone
	{
		FString UnitId;
		FBox2D Box;             // cm, Unreal XY
		float Ceiling = 280.f;  // cm
		FVector2D UnitXY = FVector2D::ZeroVector;
		FVector Outlet = FVector::ZeroVector;
		FVector Dir = FVector(1, 0, 0);
		float AirflowLps = 240.f;
		float Temp = NAN;
		float Drop = 0.f;
		bool bHidden = false;
		UProceduralMeshComponent* Mesh = nullptr;
		UMaterialInstanceDynamic* Mid = nullptr;
	};
	struct FPath
	{
		int32 Zone = 0;
		int32 Index = 0;  // loop index within its zone (staggers the dots)
		TArray<FVector> Samples;
		int32 First = 0;
	};

	bool Inside(const FVector2D& P) const;
	FVector Contain(const FZone& Z, FVector P, float Top) const;
	void LayoutZone(int32 ZoneIndex);
	void BuildMask();
	void BuildMist();
	void UpdateDrivers();
	void ApplyVisibility();

	TArray<FZone> Zones;
	TArray<FPath> Paths;
	TArray<FTransform> Xf;
	TArray<FVector2D> Outline;  // cm, Unreal XY
	FBox2D OutlineBox;
	float SplitX = 0.f;
	TSharedPtr<FJsonObject> Thermal;
	FString DriverName = TEXT("none");
	TWeakObjectPtr<ATwinViewerDirector> Director;
	double Clock = 0.0;
	double LastTelemetry = -1.0;
	float JetGain = 1.f;
	float MistGain = 1.15f;
	float MistDensity = 1.f;
	int32 DotsPerPath = 8;
	bool bShown = false;
	bool bMistShown = true;
	bool bParamsOk = false;
	int32 Visible = 0;

	UPROPERTY(Transient)
	TObjectPtr<UMaterialInterface> MistBase;
	UPROPERTY(Transient)
	TObjectPtr<UTexture2D> Mask;
	UPROPERTY(Transient)
	TArray<TObjectPtr<UProceduralMeshComponent>> MistMeshes;
	UPROPERTY(Transient)
	TArray<TObjectPtr<UMaterialInstanceDynamic>> MistMaterials;
	FIntPoint MaskDims = FIntPoint::ZeroValue;
	FVector4f MaskRect = FVector4f(0, 0, 1, 1);
};
