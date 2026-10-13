#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "TwinAirflowActor.generated.h"

class ATwinViewerDirector;
class FJsonObject;
class UInstancedStaticMeshComponent;

/**
 * Airflow ILLUSTRATION (not measured, not CFD): small tracer dots travel along schematic paths from each
 * unit's supply diffusers (or its casing outlet) along the ceiling, down into the room and back to its return
 * grille. Paths come from terminal positions, throw patterns and design airflow (GET .../hvac/airflow);
 * dot colour runs from the unit's supply to return air temperature as reported by the telemetry feed
 * (live BMS or the simulated BMS, which the HUD names), speed follows the fan speed, and a unit that is off
 * shows no dots. The HUD keeps a banner on screen whenever this layer is visible.
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
	int32 NumPaths() const { return Paths.Num(); }
	int32 NumDots() const { return Xf.Num(); }
	int32 NumUnits() const { return Units; }
	int32 VisibleDots() const { return Visible; }

	virtual void Tick(float DeltaSeconds) override;
	virtual void BeginPlay() override;

	UPROPERTY(VisibleAnywhere, Category = "Twin")
	TObjectPtr<UInstancedStaticMeshComponent> Dots;

	static FLinearColor TemperatureColor(float C);

private:
	struct FPath
	{
		FString UnitId;
		TArray<FVector> Points;
		float Length = 0.f;
		int32 First = 0;
		int32 Count = 0;
	};

	TArray<FPath> Paths;
	TArray<FTransform> Xf;
	TWeakObjectPtr<ATwinViewerDirector> Director;
	double Clock = 0.0;
	bool bShown = false;
	int32 Units = 0;
	int32 Visible = 0;
};
