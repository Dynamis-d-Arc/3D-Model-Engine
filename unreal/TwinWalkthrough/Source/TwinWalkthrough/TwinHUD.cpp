#include "TwinHUD.h"

#include "Engine/Canvas.h"
#include "Engine/Engine.h"
#include "Engine/Font.h"
#include "TwinMeta.h"
#include "TwinPlayerController.h"

void ATwinHUD::Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent)
{
	UFont* Font = GEngine->GetMediumFont();
	const float LineH = 22.f;
	const float H = 14.f + Lines.Num() * LineH;
	DrawRect(FLinearColor(0.02f, 0.03f, 0.05f, 0.62f), X, Y, W, H);
	DrawRect(Accent, X, Y, 4.f, H);
	for (int32 i = 0; i < Lines.Num(); ++i)
	{
		DrawText(Lines[i], i == 0 ? FLinearColor::White : FLinearColor(0.85f, 0.87f, 0.9f), X + 14.f, Y + 8.f + i * LineH,
		         Font, i == 0 ? 1.05f : 0.95f);
	}
}

void ATwinHUD::DrawBox(const FBox& Box, const FLinearColor& Color)
{
	FVector C[8];
	for (int32 i = 0; i < 8; ++i)
	{
		const FVector P((i & 1) ? Box.Max.X : Box.Min.X, (i & 2) ? Box.Max.Y : Box.Min.Y, (i & 4) ? Box.Max.Z : Box.Min.Z);
		C[i] = Project(P, false);
		if (C[i].Z <= 0.f)
		{
			return;  // behind the camera
		}
	}
	static const int32 Edges[12][2] = {{0, 1}, {1, 3}, {3, 2}, {2, 0}, {4, 5}, {5, 7}, {7, 6}, {6, 4}, {0, 4}, {1, 5}, {2, 6}, {3, 7}};
	for (const auto& E : Edges)
	{
		DrawLine(C[E[0]].X, C[E[0]].Y, C[E[1]].X, C[E[1]].Y, Color, 2.f);
	}
}

void ATwinHUD::DrawHUD()
{
	Super::DrawHUD();
	ATwinPlayerController* PC = Cast<ATwinPlayerController>(GetOwningPlayerController());
	if (!PC || !Canvas)
	{
		return;
	}
	const float CX = Canvas->ClipX * 0.5f, CY = Canvas->ClipY * 0.5f;
	DrawLine(CX - 9.f, CY, CX + 9.f, CY, FLinearColor(1, 1, 1, 0.9f), 2.f);
	DrawLine(CX, CY - 9.f, CX, CY + 9.f, FLinearColor(1, 1, 1, 0.9f), 2.f);
	const FLinearColor Accent(0.2f, 0.45f, 0.75f, 1.f);

	// project + current room
	TArray<FString> Top;
	const AActor* Info = PC->ProjectInfo.Get();
	Top.Add(Info ? TwinMeta::Get(Info, TEXT("Name")) : FString(TEXT("Floor Plan Twin")));
	if (const AActor* Floor = PC->CurrentRoomFloor.Get())
	{
		Top.Add(FString::Printf(TEXT("Room: %s"), *TwinMeta::Get(Floor, TEXT("RoomName"))));
		Top.Add(FString::Printf(TEXT("Clear size %s  |  %s"), *TwinMeta::Get(Floor, TEXT("Dims")), *TwinMeta::Get(Floor, TEXT("Area"))));
		Top.Add(FString::Printf(TEXT("Floor: %s"), *TwinMeta::Get(Floor, TEXT("FloorMaterial"))));
	}
	else
	{
		Top.Add(TEXT("Outside the rooms"));
	}
	Top.Add(FString::Printf(TEXT("Mode: %s"), *PC->ModeName()));
	Panel(18.f, 18.f, 430.f, Top, Accent);

	// selection
	if (AActor* Sel = PC->Selected.Get())
	{
		DrawBox(Sel->GetComponentsBoundingBox(true), FLinearColor(1.f, 0.75f, 0.1f, 1.f));
		TArray<FString> Lines;
		const FString Label = TwinMeta::Get(Sel, TEXT("Label"));
		Lines.Add(Label.IsEmpty() ? TwinMeta::Get(Sel, TEXT("Name")) : Label);
		Lines.Add(FString::Printf(TEXT("Type: %s"), *TwinMeta::Get(Sel, TEXT("Kind"))));
		const FString Size = TwinMeta::Get(Sel, TEXT("Size"));
		if (!Size.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Size: %s"), *Size));
		}
		const FString Asset = TwinMeta::Get(Sel, TEXT("Asset"));
		if (!Asset.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Asset: %s  (%s)"), *Asset, *TwinMeta::Get(Sel, TEXT("Id"))));
		}
		const FString Room = TwinMeta::Get(Sel, TEXT("RoomName"));
		if (!Room.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Room: %s"), *Room));
		}
		Lines.Add(TwinMeta::Get(Sel, TEXT("Name")));
		Panel(Canvas->ClipX - 470.f, Canvas->ClipY - 40.f - Lines.Num() * 22.f, 452.f, Lines, FLinearColor(1.f, 0.75f, 0.1f, 1.f));
	}

	// help / status
	if (PC->bShowHelp)
	{
		Panel(18.f, Canvas->ClipY - 200.f, 520.f, {
			TEXT("Controls"),
			TEXT("WASD move | mouse/arrows look | Shift run | Space jump"),
			TEXT("F fly (E/Q up/down) | G ghost | C ceilings | Tab / 1-9 rooms"),
			TEXT("Left click select | Right click walk there | X clear"),
			TEXT("P screenshot | H hide help | Esc quit"),
		}, Accent);
	}
	if (!PC->Status.IsEmpty() && GetWorld()->GetTimeSeconds() < PC->StatusUntil)
	{
		Panel(CX - 220.f, 24.f, 440.f, {PC->Status}, Accent);
	}
}
